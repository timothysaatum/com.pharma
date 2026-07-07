"""
Test suite for Reports Service
Tests the various report generation functions
"""

import pytest
import pytest_asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal
import uuid
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sales.sales_model import Sale, SaleItem
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch
from app.models.inventory.inventory_model import Drug, DrugCategory
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.customer.customer_model import Customer
from app.models.user.user_model import User
from app.models.pricing.pricing_model import PriceContract
from app.services.reports.reports_service import ReportsService


@pytest.mark.asyncio
class TestReportsService:
    """Test suite for Reports Service."""

    async def test_daily_sales_summary(self, db: AsyncSession, sales_data):
        """Test daily sales summary aggregation."""
        org_id = sales_data["org_id"]
        
        # Get today's date
        start_date = date.today()
        end_date = date.today()

        result = await ReportsService.get_daily_sales_summary(
            db=db,
            organization_id=org_id,
            start_date=start_date,
            end_date=end_date,
        )

        assert result.total > 0
        assert result.items[0].transaction_count > 0
        assert result.items[0].net_revenue > 0
        assert result.items[0].total_items > 0

    async def test_daily_sales_summary_with_filters(self, db: AsyncSession, sales_data):
        """Test daily sales summary with branch and contract filters."""
        org_id = sales_data["org_id"]
        branch_id = sales_data["branch_id"]
        contract_id = sales_data["contract_id"]
        
        start_date = date.today()
        end_date = date.today()

        # Filter by branch
        result_by_branch = await ReportsService.get_daily_sales_summary(
            db=db,
            organization_id=org_id,
            start_date=start_date,
            end_date=end_date,
            branch_id=branch_id,
        )

        # All results should be for the filtered branch
        if result_by_branch.items:
            for row in result_by_branch.items:
                assert str(row.branch_id) == str(branch_id)

    async def test_contract_performance_report(self, db: AsyncSession, sales_data):
        """Test contract performance metrics."""
        org_id = sales_data["org_id"]
        
        start_date = date.today() - timedelta(days=30)
        end_date = date.today()

        result = await ReportsService.get_contract_performance(
            db=db,
            organization_id=org_id,
            start_date=start_date,
            end_date=end_date,
        )

        # Result should include contracts with sales
        assert isinstance(result, list)
        if result:
            for row in result:
                assert "contract_name" in row
                assert "revenue" in row
                assert "discount_given" in row

    async def test_inventory_alerts(self, db: AsyncSession, sales_data):
        """Test inventory alerts for low stock and expiry."""
        org_id = sales_data["org_id"]

        # Get low stock alerts
        result = await ReportsService.get_inventory_alerts(
            db=db,
            organization_id=org_id,
            alert_types=["LOW_STOCK", "EXPIRING_SOON", "EXPIRED"],
        )

        assert isinstance(result, list)
        # Each alert should have the required fields
        for alert in result:
            assert "drug_name" in alert
            assert "alert_type" in alert
            assert alert["alert_type"] in ["LOW_STOCK", "EXPIRING_SOON", "EXPIRED"]

    async def test_top_customers_report(self, db: AsyncSession, sales_data):
        """Test top customers report."""
        org_id = sales_data["org_id"]
        
        start_date = date.today() - timedelta(days=30)
        end_date = date.today()

        result = await ReportsService.get_top_customers(
            db=db,
            organization_id=org_id,
            start_date=start_date,
            end_date=end_date,
            limit=10,
        )

        assert isinstance(result, list)
        if result:
            # Should be sorted by total_spent (descending)
            for i in range(len(result) - 1):
                assert result[i]["total_spent"] >= result[i + 1]["total_spent"]

    async def test_drug_turnover_report(self, db: AsyncSession, sales_data):
        """Test drug turnover metrics."""
        org_id = sales_data["org_id"]
        
        start_date = date.today() - timedelta(days=30)
        end_date = date.today()

        result = await ReportsService.get_drug_turnover(
            db=db,
            organization_id=org_id,
            start_date=start_date,
            end_date=end_date,
        )

        assert result.total > 0
        if result.items:
            # Should include drug metrics
            for row in result.items:
                assert row.drug_name
                assert row.units_sold > 0
                assert row.revenue > 0

    async def test_drug_turnover_returns_category_name_not_uuid(
        self,
        db: AsyncSession,
        sales_data,
    ):
        """Endpoint response validation expects category to be a string."""
        org_id = sales_data["org_id"]
        category = DrugCategory(
            id=uuid.uuid4(),
            organization_id=org_id,
            name="Analgesics",
        )
        drug = await db.get(Drug, sales_data["drug_id"])
        drug.category_id = category.id
        db.add(category)
        await db.commit()

        result = await ReportsService.get_drug_turnover(
            db=db,
            organization_id=org_id,
            start_date=date.today() - timedelta(days=30),
            end_date=date.today(),
        )

        assert result.total > 0
        assert result.items[0].category == "Analgesics"


# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="function")
async def sales_data(db: AsyncSession, setup_test_data):
    """Create test sales data for report tests."""
    org, branch, user, drugs, customer = setup_test_data
    drug = drugs[0]

    contract = PriceContract(
        id=uuid.uuid4(),
        organization_id=org.id,
        contract_code="TEST001",
        contract_name="Test Contract",
        contract_type="standard",
        discount_type="percentage",
        discount_percentage=Decimal("10.00"),
        effective_from=date.today() - timedelta(days=1),
        status="active",
        is_active=True,
        is_deleted=False,
        created_by=user.id,
    )
    
    # Create a sale for today
    sale = Sale(
        id=uuid.uuid4(),
        organization_id=org.id,
        branch_id=branch.id,
        sale_number="SALE001",
        customer_id=customer.id,
        cashier_id=user.id,
        price_contract_id=contract.id,
        subtotal=Decimal("500.00"),
        discount_amount=Decimal("50.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("450.00"),
        payment_method="cash",
        payment_status="completed",
        amount_paid=Decimal("450.00"),
        status="completed",
        created_at=datetime.now(),
    )
    
    sale_item = SaleItem(
        id=uuid.uuid4(),
        sale_id=sale.id,
        drug_id=drug.id,
        drug_name=drug.name,
        quantity=10,
        unit_price=Decimal("50.00"),
        subtotal=Decimal("500.00"),
        discount_percentage=Decimal("10.00"),
        discount_amount=Decimal("50.00"),
        tax_rate=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total_price=Decimal("450.00"),
    )
    
    # Create low stock inventory
    inventory = BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drug.id,
        quantity=5,  # Below reorder level of 10
        reserved_quantity=0,
        selling_price=Decimal("50.00"),
    )
    
    db.add_all([
        contract, sale, sale_item, inventory
    ])
    await db.commit()
    
    return {
        "org_id": org.id,
        "branch_id": branch.id,
        "user_id": user.id,
        "drug_id": drug.id,
        "customer_id": customer.id,
        "contract_id": contract.id,
        "sale_id": sale.id,
    }
