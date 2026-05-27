"""
Comprehensive test suite for Sales Service
Tests cover: process_sale, refund_sale, cancel_sale operations
"""

import pytest
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
import uuid
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.sales.sales_model import Sale, SaleItem
from app.models.inventory.branch_inventory import BranchInventory, DrugBatch, StockAdjustment
from app.models.inventory.inventory_model import Drug
from app.models.pharmacy.pharmacy_model import Branch, Organization
from app.models.customer.customer_model import Customer
from app.models.user.user_model import User
from app.models.precriptions.prescription_model import Prescription
from app.models.pricing.pricing_model import PriceContract
from app.schemas.sales_schemas import SaleCreate, SaleItemCreate, RefundSaleRequest, SaleResponse
from app.services.sales.sales_service import SalesService


@pytest.mark.asyncio
class TestProcessSale:
    """Test suite for processing sales transactions."""

    async def test_process_sale_basic(self, db: AsyncSession, setup_test_data):
        """Test basic sale processing with valid data."""
        org, branch, user, drugs, customer = setup_test_data
        
        # Create drug batch with sufficient stock
        batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drugs[0].id,
            branch_id=branch.id,
            batch_number="BATCH001",
            batch_expiry_date=date.today() + timedelta(days=365),
            quantity_received=100,
            quantity_available=100,
            remaining_quantity=100,
        )
        db.add(batch)
        
        # Create branch inventory
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=100,
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        db.add(inventory)
        await db.commit()

        # Process sale
        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=None,
            customer_id=customer.id,
            customer_name="Test Customer",
            items=[
                SaleItemCreate(
                    drug_id=drugs[0].id,
                    quantity=5,
                    requires_prescription=False,
                    prescription_verified=False,
                )
            ],
            payment_method="cash",
            amount_paid=Decimal("250.00"),
        )

        response = await SalesService.process_sale(db, sale_data, user)

        assert response.success
        assert response.sale.status == "completed"
        assert response.inventory_updated == 1
        assert response.sale.total_amount == Decimal("250.00")

    async def test_sale_response_items_count_from_loaded_items(self, db: AsyncSession, setup_test_data):
        """SaleResponse should populate items_count when sale items are present."""
        org, branch, user, drugs, customer = setup_test_data

        sale = Sale(
            id=uuid.uuid4(),
            organization_id=org.id,
            branch_id=branch.id,
            sale_number="SALE_COUNT_TEST",
            customer_id=customer.id,
            cashier_id=user.id,
            subtotal=Decimal("150.00"),
            discount_amount=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_amount=Decimal("150.00"),
            payment_method="cash",
            payment_status="completed",
            amount_paid=Decimal("150.00"),
            status="completed",
        )

        sale_item = SaleItem(
            id=uuid.uuid4(),
            sale_id=sale.id,
            drug_id=drugs[0].id,
            quantity=3,
            unit_price=Decimal("50.00"),
            subtotal=Decimal("150.00"),
            discount_percentage=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            tax_rate=Decimal("0.00"),
            tax_amount=Decimal("0.00"),
            total_price=Decimal("150.00"),
        )

        db.add_all([sale, sale_item])
        await db.commit()

        loaded_sale = await db.execute(
            select(Sale)
            .options(selectinload(Sale.items))
            .where(Sale.id == sale.id)
        )
        loaded_sale = loaded_sale.scalar_one()

        response = SaleResponse.model_validate(loaded_sale)

        assert response.items_count == 3

    async def test_process_sale_insufficient_stock(self, db: AsyncSession, setup_test_data):
        """Test sale fails when insufficient stock available."""
        org, branch, user, drugs, customer = setup_test_data
        
        # Create inventory with limited stock
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=2,  # Only 2 units
            reserved_quantity=0,
            selling_price=Decimal("50.00"),
        )
        db.add(inventory)
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=None,
            customer_id=customer.id,
            customer_name="Test Customer",
            items=[
                SaleItemCreate(
                    drug_id=drugs[0].id,
                    quantity=5,  # Request 5, only 2 available
                    requires_prescription=False,
                    prescription_verified=False,
                )
            ],
            payment_method="cash",
            amount_paid=Decimal("250.00"),
        )

        with pytest.raises(Exception) as exc:
            await SalesService.process_sale(db, sale_data, user)
        
        assert "Insufficient stock" in str(exc.value)

    async def test_process_sale_with_prescription(self, db: AsyncSession, setup_test_data):
        """Test sale of prescription drug requires valid prescription."""
        org, branch, user, drugs, customer = setup_test_data
        
        # Mark drug as requiring prescription
        drugs[0].requires_prescription = True
        
        # Create prescription
        rx = Prescription(
            id=uuid.uuid4(),
            customer_id=customer.id,
            drug_id=drugs[0].id,
            quantity_prescribed=10,
            quantity_dispensed=0,
            refills_remaining=2,
            expiry_date=date.today() + timedelta(days=30),
            status="active",
            organization_id=org.id,
        )
        
        # Setup inventory and batch
        batch = DrugBatch(
            id=uuid.uuid4(),
            drug_id=drugs[0].id,
            branch_id=branch.id,
            batch_number="RX001",
            batch_expiry_date=date.today() + timedelta(days=365),
            quantity_received=50,
            quantity_available=50,
            remaining_quantity=50,
        )
        
        inventory = BranchInventory(
            id=uuid.uuid4(),
            branch_id=branch.id,
            drug_id=drugs[0].id,
            quantity=50,
            reserved_quantity=0,
            selling_price=Decimal("100.00"),
        )
        
        db.add_all([rx, batch, inventory])
        await db.commit()

        sale_data = SaleCreate(
            branch_id=branch.id,
            price_contract_id=None,
            customer_id=customer.id,
            customer_name="Test Customer",
            items=[
                SaleItemCreate(
                    drug_id=drugs[0].id,
                    quantity=3,
                    requires_prescription=True,
                    prescription_verified=True,
                )
            ],
            payment_method="cash",
            amount_paid=Decimal("300.00"),
            prescription_id=rx.id,
        )

        response = await SalesService.process_sale(db, sale_data, user)
        assert response.success


@pytest.mark.asyncio
class TestRefundSale:
    """Test suite for refunding sales."""

    async def test_refund_sale_full(self, db: AsyncSession, setup_test_data, completed_sale):
        """Test full refund of a completed sale."""
        sale, user = completed_sale
        
        refund_data = RefundSaleRequest(
            reason="Customer returned item",
            items_to_refund=[
                {
                    "sale_item_id": sale.items[0].id,
                    "quantity": sale.items[0].quantity,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=sale.total_amount,
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        response = await SalesService.refund_sale(db, sale.id, refund_data, user)

        assert response.success
        assert response.inventory_restored == 1
        assert response.refund_amount == sale.total_amount

    async def test_refund_sale_partial(self, db: AsyncSession, setup_test_data, completed_sale):
        """Test partial refund of specific items."""
        sale, user = completed_sale
        
        # Refund only half the quantity
        refund_qty = sale.items[0].quantity // 2
        refund_amount = (sale.items[0].total_price / sale.items[0].quantity) * refund_qty

        refund_data = RefundSaleRequest(
            reason="Partial return",
            items_to_refund=[
                {
                    "sale_item_id": sale.items[0].id,
                    "quantity": refund_qty,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=refund_amount,
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        response = await SalesService.refund_sale(db, sale.id, refund_data, user)

        assert response.success
        assert response.refund_amount == refund_amount

    async def test_refund_exceeds_sale_total(self, db: AsyncSession, setup_test_data, completed_sale):
        """Test refund cannot exceed sale total."""
        sale, user = completed_sale
        
        refund_data = RefundSaleRequest(
            reason="Invalid refund",
            items_to_refund=[
                {
                    "sale_item_id": sale.items[0].id,
                    "quantity": sale.items[0].quantity,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=sale.total_amount + Decimal("100.00"),  # Exceeds total
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        with pytest.raises(Exception) as exc:
            await SalesService.refund_sale(db, sale.id, refund_data, user)
        
        assert "cannot exceed" in str(exc.value)

    async def test_refund_restores_inventory(self, db: AsyncSession, completed_sale):
        """Test refund properly restores inventory."""
        sale, user = completed_sale
        
        # Get initial inventory
        initial_inventory = await db.execute(
            f"SELECT quantity FROM branch_inventory WHERE drug_id = '{sale.items[0].drug_id}'"
        )
        initial_qty = initial_inventory.scalar()

        refund_data = RefundSaleRequest(
            reason="Test refund",
            items_to_refund=[
                {
                    "sale_item_id": sale.items[0].id,
                    "quantity": sale.items[0].quantity,
                    "reason": "Return",
                    "restock": True,
                }
            ],
            refund_amount=sale.total_amount,
            refund_method="cash",
            manager_approval_user_id=user.id,
        )

        await SalesService.refund_sale(db, sale.id, refund_data, user)

        # Check inventory increased
        updated_inventory = await db.execute(
            f"SELECT quantity FROM branch_inventory WHERE drug_id = '{sale.items[0].drug_id}'"
        )
        updated_qty = updated_inventory.scalar()

        assert updated_qty == initial_qty + sale.items[0].quantity


@pytest.mark.asyncio  
class TestInventoryDeduction:
    """Test suite for inventory deduction during sales."""

    async def test_fefo_batch_selection(self, db: AsyncSession):
        """Test FEFO (First Expire, First Out) batch selection for inventory."""
        # Create multiple batches with different expiry dates
        # The service should deduct from earliest expiring batch first
        pass

    async def test_inventory_reserved_during_sale(self, db: AsyncSession):
        """Test inventory is properly reserved during sale processing."""
        pass


# ─── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
async def setup_test_data(db: AsyncSession):
    """Create test data: organization, branch, user, drugs, customer."""
    org = Organization(
        id=uuid.uuid4(),
        organization_name="Test Pharmacy",
        tax_id="123456789",
    )
    
    branch = Branch(
        id=uuid.uuid4(),
        organization_id=org.id,
        name="Test Branch",
        code="TB001",
        is_active=True,
        is_deleted=False,
    )
    
    user = User(
        id=uuid.uuid4(),
        organization_id=org.id,
        user_name="test_user",
        email="test@pharmacy.com",
        hashed_password="hashed_pwd",
        role="pharmacist",
        is_active=True,
        assigned_branches=[branch.id],
    )
    
    drugs = [
        Drug(
            id=uuid.uuid4(),
            organization_id=org.id,
            drug_name=f"Drug {i}",
            sku=f"SKU{i:03d}",
            unit_price=Decimal("50.00"),
            reorder_level=10,
            is_active=True,
            is_deleted=False,
        )
        for i in range(3)
    ]
    
    customer = Customer(
        id=uuid.uuid4(),
        organization_id=org.id,
        customer_name="Test Customer",
        phone="0501234567",
        loyalty_tier="standard",
        loyalty_points=0,
    )
    
    db.add(org)
    db.add(branch)
    db.add(user)
    db.add_all(drugs)
    db.add(customer)
    await db.commit()
    
    return org, branch, user, drugs, customer


@pytest.fixture
async def completed_sale(db: AsyncSession, setup_test_data):
    """Create a completed sale for testing refunds."""
    org, branch, user, drugs, customer = setup_test_data
    
    # Setup inventory and batch
    batch = DrugBatch(
        id=uuid.uuid4(),
        drug_id=drugs[0].id,
        branch_id=branch.id,
        batch_number="TEST001",
        batch_expiry_date=date.today() + timedelta(days=365),
        quantity_received=100,
        quantity_available=95,  # 5 sold
        remaining_quantity=95,
    )
    
    inventory = BranchInventory(
        id=uuid.uuid4(),
        branch_id=branch.id,
        drug_id=drugs[0].id,
        quantity=95,
        reserved_quantity=0,
        selling_price=Decimal("50.00"),
    )
    
    sale = Sale(
        id=uuid.uuid4(),
        organization_id=org.id,
        branch_id=branch.id,
        sale_number="SALE001",
        customer_id=customer.id,
        cashier_id=user.id,
        subtotal=Decimal("250.00"),
        discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total_amount=Decimal("250.00"),
        payment_method="cash",
        payment_status="completed",
        amount_paid=Decimal("250.00"),
        status="completed",
    )
    
    sale_item = SaleItem(
        id=uuid.uuid4(),
        sale_id=sale.id,
        drug_id=drugs[0].id,
        quantity=5,
        batch_id=batch.id,
        unit_price=Decimal("50.00"),
        subtotal=Decimal("250.00"),
        total_discount_amount=Decimal("0.00"),
        tax_amount=Decimal("0.00"),
        total_price=Decimal("250.00"),
    )
    
    db.add_all([batch, inventory, sale, sale_item])
    await db.commit()
    
    return sale, user
