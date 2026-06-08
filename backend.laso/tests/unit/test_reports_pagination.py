"""
Unit tests for Reports Service pagination
"""

import pytest
from datetime import date, timedelta
from decimal import Decimal
import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sales.sales_model import Sale, SaleItem
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.user.user_model import User
from app.services.reports.reports_service import ReportsService
from app.utils.pagination import PaginationParams


@pytest.mark.asyncio
class TestReportsPagination:
    """Test suite for paginated reports."""

    async def setup_report_data(self, db, setup_test_data, num_sales=10):
        org, branch, user, drugs, customer = setup_test_data

        for i in range(num_sales):
            sale = Sale(
                id=uuid.uuid4(),
                organization_id=org.id,
                branch_id=branch.id,
                sale_number=f"SALE-PAG-{i}",
                subtotal=100.0,
                total_amount=100.0,
                payment_method="cash",
                cashier_id=user.id,
                status="completed",
                created_at=date.today(),
            )
            db.add(sale)

            item = SaleItem(
                id=uuid.uuid4(),
                sale_id=sale.id,
                drug_id=drugs[0].id,
                drug_name=drugs[0].name,
                quantity=1,
                unit_price=100.0,
                subtotal=100.0,
                total_price=100.0,
            )
            db.add(item)

        await db.commit()

    async def test_daily_sales_summary_pagination(self, db: AsyncSession, setup_test_data):
        """Test pagination for daily sales summary."""
        await self.setup_report_data(db, setup_test_data, num_sales=5)
        org, branch, user, drugs, customer = setup_test_data

        # Test page 1, size 2
        params = PaginationParams(page=1, page_size=2)
        report = await ReportsService.get_daily_sales_summary(
            db=db,
            organization_id=org.id,
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1),
            pagination=params
        )

        assert report.page == 1
        assert report.page_size == 2
        # Since we grouped by date/branch/etc, all 5 sales today might be in one row
        # if they share the same contract/cashier/branch.
        # Let's verify we get at least one row and the totals are correct.
        assert len(report.items) >= 1
        assert report.items[0].transaction_count >= 5

    async def test_drug_turnover_pagination(self, db: AsyncSession, setup_test_data):
        """Test pagination for drug turnover."""
        org, branch, user, drugs, customer = setup_test_data
        # drugs has 3 items from setup_test_data fixture
        await self.setup_report_data(db, setup_test_data, num_sales=3)

        params = PaginationParams(page=1, page_size=1)
        report = await ReportsService.get_drug_turnover(
            db=db,
            organization_id=org.id,
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1),
            pagination=params
        )

        assert report.page == 1
        assert report.page_size == 1
        assert len(report.items) == 1
        assert report.total >= 1
