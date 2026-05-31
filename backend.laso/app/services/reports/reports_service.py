"""
Reports Service
===============
Provides analytics and reporting endpoints for sales, inventory, and financial data.

Reports Support:
1. Daily Sales Summary — revenue by date, branch, contract, cashier
2. Contract Performance — discount usage, sales metrics by contract
3. Inventory Alerts — low stock, expiry warnings
4. Customer History — loyalty tier, top customers
5. Drug Turnover — units sold, revenue by drug and period
"""

from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Any
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select, func, and_, case, String, Integer, Float
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.sales.sales_model import Sale, SaleItem
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch, StockAdjustment
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.customer.customer_model import Customer
from app.models.pricing.pricing_model import PriceContract
from app.models.system_md.sys_models import SystemAlert
from app.models.user.user_model import User
from app.utils.pagination import PaginatedResponse, Paginator, PaginationParams
from app.schemas.reports_schemas import DailySalesSummaryRow, DrugTurnoverRow


class ReportsService:
    """Stateless reports service."""

    @staticmethod
    async def get_daily_sales_summary(
        db: AsyncSession,
        organization_id: uuid.UUID,
        start_date: date,
        end_date: date,
        branch_id: Optional[uuid.UUID] = None,
        contract_id: Optional[uuid.UUID] = None,
        cashier_id: Optional[uuid.UUID] = None,
        pagination: Optional[PaginationParams] = None,
    ) -> PaginatedResponse[DailySalesSummaryRow]:
        """
        Daily sales summary grouped by date, branch, contract, and cashier.
        
        Returns aggregated sales metrics:
        - Transaction count
        - Gross revenue (subtotal)
        - Total discounts
        - Total tax
        - Net revenue
        - Refund count
        """
        stmt = (
            select(
                func.cast(func.date(Sale.created_at), String).label("sale_date"),
                func.cast(Sale.branch_id, String).label("branch_id"),
                Branch.name.label("branch_name"),
                func.cast(Sale.price_contract_id, String).label("contract_id"),
                PriceContract.contract_name,
                func.cast(Sale.cashier_id, String).label("cashier_id"),
                User.full_name.label("cashier_name"),
                func.count(func.distinct(Sale.id)).label("transaction_count"),
                func.cast(func.sum(Sale.subtotal), Float).label("gross_revenue"),
                func.cast(func.sum(Sale.discount_amount), Float).label("total_discount"),
                func.cast(func.sum(Sale.tax_amount), Float).label("total_tax"),
                func.cast(func.sum(Sale.total_amount), Float).label("net_revenue"),
                func.cast(func.coalesce(func.sum(SaleItem.quantity), 0), Integer).label("total_items"),
                func.cast(func.sum(
                    case(
                        (Sale.status == "refunded", 1),
                        else_=0,
                    )
                ), Integer).label("refund_count"),
            )
            .join(Branch, Sale.branch_id == Branch.id)
            .join(User, Sale.cashier_id == User.id)
            .outerjoin(
                PriceContract,
                Sale.price_contract_id == PriceContract.id,
            )
            # Use an outerjoin to SaleItem so sales without explicit item rows
            # (edge cases) are still included. We aggregate items via SUM
            # and protect against NULL using COALESCE above.
            .outerjoin(SaleItem, SaleItem.sale_id == Sale.id)
            .where(
                Sale.organization_id == organization_id,
                func.date(Sale.created_at) >= start_date,
                func.date(Sale.created_at) <= end_date,
                Sale.status == "completed",  # Only completed sales
            )
        )

        if branch_id:
            stmt = stmt.where(Sale.branch_id == branch_id)
        if contract_id:
            stmt = stmt.where(Sale.price_contract_id == contract_id)
        if cashier_id:
            stmt = stmt.where(Sale.cashier_id == cashier_id)

        stmt = stmt.group_by(
            func.date(Sale.created_at),
            Sale.branch_id,
            Branch.name,
            Sale.price_contract_id,
            PriceContract.contract_name,
            Sale.cashier_id,
            User.full_name,
        ).order_by(func.date(Sale.created_at).desc())

        paginator = Paginator(db)
        result = await paginator.paginate(stmt, pagination or PaginationParams())

        # Manually validate to schema to ensure complex grouping returns
        # full result rows instead of just scalars.
        items = []
        # Re-execute paginated query to get rows
        paginated_stmt = stmt.offset((pagination.page - 1) * pagination.page_size if pagination else 0).limit(pagination.page_size if pagination else 50)
        res = await db.execute(paginated_stmt)
        for row in res.all():
            items.append(DailySalesSummaryRow.model_validate(row))

        result.items = items
        return result

    @staticmethod
    async def get_contract_performance(
        db: AsyncSession,
        organization_id: uuid.UUID,
        start_date: date,
        end_date: date,
        contract_id: Optional[uuid.UUID] = None,
    ) -> List[Dict[str, Any]]:
        """
        Contract performance metrics: revenue, discount usage, customer count.
        """
        stmt = (
            select(
                PriceContract.id,
                PriceContract.contract_code,
                PriceContract.contract_name,
                PriceContract.contract_type,
                func.count(func.distinct(Sale.id)).label("sales_count"),
                func.sum(Sale.total_amount).label("revenue"),
                func.sum(Sale.discount_amount).label("discount_given"),
                func.avg(Sale.discount_amount).label("avg_discount"),
                func.count(func.distinct(Sale.customer_id)).label("customer_count"),
            )
            .select_from(PriceContract)
            .join(Sale, Sale.price_contract_id == PriceContract.id)
            .where(
                PriceContract.organization_id == organization_id,
                func.date(Sale.created_at) >= start_date,
                func.date(Sale.created_at) <= end_date,
                Sale.status == "completed",
            )
        )

        if contract_id:
            stmt = stmt.where(PriceContract.id == contract_id)

        stmt = stmt.group_by(
            PriceContract.id,
            PriceContract.contract_code,
            PriceContract.contract_name,
            PriceContract.contract_type,
        ).order_by(func.sum(Sale.total_amount).desc().nullsfirst())

        result = await db.execute(stmt)
        rows = result.fetchall()

        return [
            {
                "contract_id": str(row.id),
                "contract_code": row.contract_code,
                "contract_name": row.contract_name,
                "contract_type": row.contract_type,
                "sales_count": row.sales_count,
                "revenue": float(row.revenue or 0),
                "discount_given": float(row.discount_given or 0),
                "avg_discount": float(row.avg_discount or 0) if row.avg_discount else 0,
                "customer_count": row.customer_count,
            }
            for row in rows
        ]

    @staticmethod
    async def get_inventory_alerts(
        db: AsyncSession,
        organization_id: uuid.UUID,
        branch_id: Optional[uuid.UUID] = None,
        alert_types: Optional[List[str]] = None,  # ['LOW_STOCK', 'EXPIRING_SOON', 'EXPIRED']
    ) -> List[Dict[str, Any]]:
        """
        Inventory alerts: low stock, expiry warnings.
        """
        if alert_types is None:
            alert_types = ["LOW_STOCK", "EXPIRING_SOON", "EXPIRED"]

        alerts = []

        # Low stock alerts
        if "LOW_STOCK" in alert_types:
            stmt = (
                select(
                    BranchInventory.id,
                    BranchInventory.branch_id,
                    Branch.name.label("branch_name"),
                    Drug.id.label("drug_id"),
                    Drug.name.label("drug_name"),
                    Drug.sku,
                    Drug.reorder_level,
                    BranchInventory.quantity,
                    func.cast(func.coalesce(None, "LOW_STOCK"), String).label("alert_type"),
                )
                .join(Branch, BranchInventory.branch_id == Branch.id)
                .join(Drug, BranchInventory.drug_id == Drug.id)
                .where(
                    Drug.organization_id == organization_id,
                    BranchInventory.quantity <= Drug.reorder_level,
                )
            )
            if branch_id:
                stmt = stmt.where(BranchInventory.branch_id == branch_id)

            result = await db.execute(stmt)
            for row in result.fetchall():
                alerts.append(
                    {
                        "inventory_id": str(row.id),
                        "branch_id": str(row.branch_id),
                        "branch_name": row.branch_name,
                        "drug_id": str(row.drug_id),
                        "drug_name": row.drug_name,
                        "drug_sku": row.sku,
                        "current_quantity": row.quantity,
                        "reorder_level": row.reorder_level,
                        "alert_type": row.alert_type,
                    }
                )

        # Expiry alerts
        today = date.today()
        thirty_days_later = today + timedelta(days=30)

        if "EXPIRING_SOON" in alert_types or "EXPIRED" in alert_types:
            stmt = (
                select(
                    DrugBatch.id,
                    DrugBatch.branch_id,
                    Branch.name.label("branch_name"),
                    Drug.id.label("drug_id"),
                    Drug.name.label("drug_name"),
                    Drug.sku,
                    DrugBatch.batch_number,
                    DrugBatch.expiry_date.label("batch_expiry_date"),
                    DrugBatch.remaining_quantity,
                    func.cast(case(
                        (DrugBatch.expiry_date < today, "EXPIRED"),
                        else_="EXPIRING_SOON"
                    ), String).label("alert_type"),
                )
                .join(Drug, DrugBatch.drug_id == Drug.id)
                .join(Branch, DrugBatch.branch_id == Branch.id)
                .where(
                    Drug.organization_id == organization_id,
                    DrugBatch.expiry_date <= thirty_days_later,
                )
            )
            if branch_id:
                stmt = stmt.where(DrugBatch.branch_id == branch_id)

            result = await db.execute(stmt)
            for row in result.fetchall():
                if row.alert_type in alert_types:
                    alerts.append(
                        {
                            "batch_id": str(row.id),
                            "branch_id": str(row.branch_id),
                            "branch_name": row.branch_name,
                            "drug_id": str(row.drug_id),
                            "drug_name": row.drug_name,
                            "drug_sku": row.sku,
                            "batch_number": row.batch_number,
                            "expiry_date": str(row.batch_expiry_date),
                            "remaining_quantity": row.remaining_quantity,
                            "alert_type": row.alert_type,
                        }
                    )

        return alerts

    @staticmethod
    async def get_top_customers(
        db: AsyncSession,
        organization_id: uuid.UUID,
        start_date: date,
        end_date: date,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """
        Top customers by revenue, loyalty tier, purchase count.
        """
        stmt = (
            select(
                Customer.id,
                func.concat(Customer.first_name, " ", Customer.last_name).label("customer_name"),
                Customer.phone,
                Customer.loyalty_tier,
                Customer.loyalty_points,
                func.count(Sale.id).label("purchase_count"),
                func.sum(Sale.total_amount).label("total_spent"),
                func.max(Sale.created_at).label("last_purchase"),
                func.count(func.distinct(func.date(Sale.created_at))).label(
                    "visit_days"
                ),
            )
            .select_from(Customer)
            .outerjoin(Sale, Customer.id == Sale.customer_id)
            .where(
                Customer.organization_id == organization_id,
                func.date(Sale.created_at) >= start_date,
                func.date(Sale.created_at) <= end_date,
            )
            .group_by(
                Customer.id,
                Customer.first_name,
                Customer.last_name,
                Customer.phone,
                Customer.loyalty_tier,
                Customer.loyalty_points,
            )
            .order_by(func.sum(Sale.total_amount).desc().nullsfirst())
            .limit(limit)
        )

        result = await db.execute(stmt)
        return [
            {
                "customer_id": str(row.id),
                "customer_name": row.customer_name,
                "phone": row.phone,
                "loyalty_tier": row.loyalty_tier,
                "loyalty_points": row.loyalty_points,
                "purchase_count": row.purchase_count,
                "total_spent": float(row.total_spent or 0),
                "last_purchase": str(row.last_purchase) if row.last_purchase else None,
                "visit_days": row.visit_days,
            }
            for row in result.fetchall()
        ]

    @staticmethod
    async def get_drug_turnover(
        db: AsyncSession,
        organization_id: uuid.UUID,
        start_date: date,
        end_date: date,
        branch_id: Optional[uuid.UUID] = None,
        pagination: Optional[PaginationParams] = None,
    ) -> PaginatedResponse[DrugTurnoverRow]:
        """
        Drug turnover: units sold, revenue, transaction count by drug and period.
        """
        stmt = (
            select(
                func.cast(Drug.id, String).label("drug_id"),
                Drug.name.label("drug_name"),
                Drug.sku.label("drug_sku"),
                Drug.category_id.label("category"),
                func.cast(func.sum(SaleItem.quantity), Integer).label("units_sold"),
                func.cast(func.sum(SaleItem.total_price), Float).label("revenue"),
                func.count(func.distinct(Sale.id)).label("transaction_count"),
                func.cast(func.avg(SaleItem.unit_price), Float).label("avg_selling_price"),
            )
            .join(SaleItem, SaleItem.drug_id == Drug.id)
            .join(Sale, SaleItem.sale_id == Sale.id)
            .where(
                Drug.organization_id == organization_id,
                func.date(Sale.created_at) >= start_date,
                func.date(Sale.created_at) <= end_date,
                Sale.status == "completed",
            )
        )

        if branch_id:
            stmt = stmt.where(Sale.branch_id == branch_id)

        stmt = (
            stmt.group_by(
                Drug.id,
                Drug.name,
                Drug.sku,
                Drug.category_id,
            )
            .order_by(func.sum(SaleItem.quantity).desc().nullsfirst())
        )

        paginator = Paginator(db)
        result = await paginator.paginate(stmt, pagination or PaginationParams())

        # Ensure full result rows are captured and validated against the schema
        items = []
        paginated_stmt = stmt.offset((pagination.page - 1) * pagination.page_size if pagination else 0).limit(pagination.page_size if pagination else 20)
        res = await db.execute(paginated_stmt)
        for row in res.all():
            items.append(DrugTurnoverRow.model_validate(row))

        result.items = items
        return result
